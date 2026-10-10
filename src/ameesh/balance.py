# SPDX-License-Identifier: AGPL-3.0-only
"""Solde des fournisseurs payés au token et dépense réelle (lot L26, R20).

Un fournisseur payé au token publie un **solde** : c'est la vérité de la
dépense, là où `turn_costs` n'a qu'une estimation (barème × jetons). On lit
ce solde (endpoint en lecture seule et gratuit : aucune dépense), on
l'horodate en base (`provider_balances`, migration 0027), et la dépense réelle
par heure et par jour est la **différence de deux soldes successifs**.

* Source remplaçable : une classe `BalanceSource` par fournisseur, avec un
  transport injectable ; les tests rejouent une réponse enregistrée, sans
  réseau.
* DeepSeek : `GET {base}/user/balance`, clé lue dans l'environnement
  (`DEEPSEEK_API_KEY`). La clé ne va que dans l'en-tête de la requête :
  jamais dans un journal, un message d'erreur, un fil ni la base.
* Transport : celui de la découverte des modèles (`discovery.http`, L14) —
  aucune redirection suivie, HTTPS exigé hors boucle locale, même origine.
* Une hausse du solde est un rechargement : elle ne compte pas comme dépense
  (et une dépense faite dans le même intervalle qu'un rechargement n'est pas
  vue — limite documentée ; un relevé fréquent la réduit).
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass
from typing import Callable, Iterable

from . import storage

#: délai d'une lecture de solde (secondes)
TIMEOUT = 10.0


class BalanceError(RuntimeError):
    """Lecture de solde impossible (sans jamais citer la clé)."""


@dataclass(frozen=True)
class BalanceReading:
    provider: str
    currency: str
    total: float
    granted: float | None = None
    topped_up: float | None = None
    available: bool | None = None


#: transport : (url, en-têtes, délai) -> (charge JSON | None, nom de l'échec | "")
#: — la forme de `discovery.http.fetch_json`, que le transport par défaut réutilise
Transport = Callable[[str, dict, float], "tuple[object | None, str]"]


def _https_transport(url: str, headers: dict, timeout: float) -> tuple[object | None, str]:
    """Le transport des sources de découverte (L14), pas un second client HTTP.

    `discovery.http.fetch_json` refuse **toute** redirection (une 3xx est une
    erreur : la clé ne suit jamais vers une autre origine), exige HTTPS hors
    boucle locale, vérifie que la réponse vient de l'origine demandée, et ne
    rend que des NOMS d'erreur — jamais un texte qui citerait l'en-tête.
    """
    from .discovery import http as discovery_http

    try:
        return discovery_http.fetch_json(url, headers=headers, timeout=timeout)
    except (discovery_http.RedirectRefused, discovery_http.InsecureEndpoint,
            discovery_http.OriginChanged) as refused:
        raise BalanceError("lecture de solde refusée (%s)" % type(refused).__name__) from None


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BalanceSource:
    """Un fournisseur dont on lit le solde. Sous-classer pour en ajouter un."""

    provider = ""

    def __init__(self, env: dict | None = None, transport: Transport | None = None):
        self.env = dict(os.environ if env is None else env)
        self.transport = transport or _https_transport

    def configured(self) -> bool:
        """La clé est-elle dans l'environnement ? (sinon : rien n'est lu)"""
        raise NotImplementedError

    def fetch(self) -> list[BalanceReading]:
        raise NotImplementedError


class DeepSeekBalance(BalanceSource):
    """`GET /user/balance` de DeepSeek : lecture seule, gratuite."""

    provider = "deepseek"
    KEY_ENV = ("DEEPSEEK_API_KEY",)
    BASE_ENV = "AMEESH_DEEPSEEK_API_BASE"
    DEFAULT_BASE = "https://api.deepseek.com"

    def _key(self) -> str:
        for name in self.KEY_ENV:
            value = (self.env.get(name) or "").strip()
            if value:
                return value
        return ""

    def configured(self) -> bool:
        return bool(self._key())

    def url(self) -> str:
        base = (self.env.get(self.BASE_ENV) or self.DEFAULT_BASE).rstrip("/")
        return base + "/user/balance"

    def fetch(self) -> list[BalanceReading]:
        key = self._key()
        if not key:
            raise BalanceError("clé absente : posez %s dans l'environnement" % self.KEY_ENV[0])
        payload, failure = self.transport(
            self.url(), {"Authorization": "Bearer " + key, "Accept": "application/json"},
            TIMEOUT)
        if failure or payload is None:
            raise BalanceError("solde illisible ou refusé par le fournisseur (%s)"
                               % (failure or "réponse vide"))
        return self.parse(payload)

    @classmethod
    def parse(cls, payload) -> list[BalanceReading]:
        """`{"is_available": …, "balance_infos": [{currency, total_balance,
        granted_balance, topped_up_balance}]}` → un relevé par devise."""
        if not isinstance(payload, dict):
            raise BalanceError("réponse de solde inattendue")
        infos = payload.get("balance_infos")
        if not isinstance(infos, list):
            raise BalanceError("réponse de solde sans balance_infos")
        available = payload.get("is_available")
        out = []
        for info in infos:
            if not isinstance(info, dict):
                continue
            total = _number(info.get("total_balance"))
            currency = str(info.get("currency") or "").strip().upper()
            if total is None or not currency:
                continue
            out.append(BalanceReading(
                cls.provider, currency, total,
                granted=_number(info.get("granted_balance")),
                topped_up=_number(info.get("topped_up_balance")),
                available=bool(available) if isinstance(available, bool) else None))
        if not out:
            raise BalanceError("réponse de solde sans montant lisible")
        return out


#: sources connues, par nom de fournisseur
SOURCES: dict[str, type[BalanceSource]] = {"deepseek": DeepSeekBalance}


def sources(env: dict | None = None, transport: Transport | None = None,
            names: Iterable[str] | None = None) -> list[BalanceSource]:
    return [SOURCES[name](env=env, transport=transport)
            for name in (names or SOURCES) if name in SOURCES]


def account_sources(cfg, *, env: dict | None = None, transport: Transport | None = None,
                    names: Iterable[str] | None = None) -> list:
    """`[(source, compte)]` : une source par compte de clé d'API déclaré (L30).

    Pour un fournisseur dont l'hôte déclare des comptes `api_key_env`
    (`ameesh.accounts`), une source PAR compte, qui porte la clé de ce compte ;
    sinon la source historique de l'environnement, compte `None`. Un profil
    inutilisable (clé absente, fichier trop ouvert) n'a pas de source : la clé
    n'est jamais lue hors d'un profil valide, ni citée.
    """
    from . import accounts
    env = dict(os.environ if env is None else env)
    out: list = []
    for name in (names or SOURCES):
        cls = SOURCES.get(name)
        if cls is None:
            continue
        try:
            items = [p for p in accounts.profiles(cfg, name) if p.kind == "api_key_env"]
        except accounts.AccountError:
            items = []
        if not items:
            out.append((cls(env=env, transport=transport), None))
            continue
        for profile in items:
            tmp: dict = {}
            try:
                accounts.apply_env(tmp, profile, environ=env)
            except accounts.AccountError:
                continue
            senv = {k: v for k, v in env.items() if k not in getattr(cls, "KEY_ENV", ())}
            senv[cls.KEY_ENV[0]] = tmp[profile.env]
            out.append((cls(env=senv, transport=transport), profile.name))
    return out


def record(db, source: BalanceSource, account: str | None = None,
           min_interval_s: float = 0.0) -> list[dict]:
    """Lit le solde et l'enregistre (une ligne par devise).

    `account` (L30, migration 0028) : le compte (clé d'API) dont la source porte
    la clé ; la série de soldes est alors celle de ce compte.

    `min_interval_s` (L71) : relevé **partagé** entre exécuteurs. Chaque
    exécuteur qui voit la clé relève le solde ; sans garde, deux exécuteurs
    écrivaient deux lignes toutes les 15 minutes. Si un relevé de ce
    fournisseur (et compte) a moins de `min_interval_s` secondes, rien n'est
    demandé au fournisseur ni écrit ; l'écriture elle-même est conditionnelle,
    pour deux relèves lancées à la même seconde.
    """
    ops = storage.of(db).operations
    if min_interval_s > 0 and ops.recent_balance(
            provider=source.provider, account=account, within_s=min_interval_s):
        return []
    rows = []
    for reading in source.fetch():
        extra: dict = {}
        if account is not None:
            extra["account"] = account
        if min_interval_s > 0:
            extra["unless_within_s"] = min_interval_s
        row = ops.record_balance(
            provider=reading.provider, currency=reading.currency, total=reading.total,
            granted=reading.granted, topped_up=reading.topped_up,
            available=reading.available, **extra)
        if row is not None:
            rows.append(row)
    return rows


def _bucket(ts: float, size: str) -> float:
    moment = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    if size == "hour":
        moment = moment.replace(minute=0, second=0, microsecond=0)
    else:
        moment = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.timestamp()


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def spend(rows: Iterable[dict]) -> dict:
    """Dépense réelle par heure et par jour (UTC), depuis des soldes horodatés.

    Pour chaque (fournisseur, devise), dans l'ordre chronologique : une baisse
    du solde est une dépense, attribuée à l'heure (et au jour) du relevé qui la
    constate ; une hausse est un rechargement, comptée à part.
    """
    # Une série par (fournisseur, devise, compte) : deux clés d'API ont deux
    # soldes, et la différence entre elles ne serait pas une dépense (L30).
    series: dict = {}
    for row in sorted(rows, key=lambda r: (r["provider"], r["currency"],
                                           r.get("account") or "", r["observed_ts"])):
        series.setdefault((row["provider"], row["currency"], row.get("account")),
                          []).append(row)

    def _account(entry: dict, account) -> dict:
        if account is not None:
            entry["account"] = account
        return entry

    out: dict = {"hourly": [], "daily": [], "topups": []}
    for size, key in (("hour", "hourly"), ("day", "daily")):
        buckets: dict = {}
        for (provider, currency, account), points in series.items():
            for prev, cur in zip(points, points[1:]):
                delta = float(prev["total"]) - float(cur["total"])
                start = _bucket(float(cur["observed_ts"]), size)
                slot = buckets.setdefault((provider, currency, account or "", start), _account({
                    "provider": provider, "currency": currency, "start_ts": start,
                    "start": _iso(start), "spent": 0.0, "readings": 0}, account))
                slot["readings"] += 1
                if delta > 0:
                    slot["spent"] = round(slot["spent"] + delta, 6)
        out[key] = [buckets[k] for k in sorted(buckets, key=lambda k: (k[3], k[0], k[1], k[2]))]
    for (provider, currency, account), points in series.items():
        for prev, cur in zip(points, points[1:]):
            delta = float(cur["total"]) - float(prev["total"])
            if delta > 0:
                out["topups"].append(_account({
                    "provider": provider, "currency": currency,
                    "at_ts": cur["observed_ts"], "at": _iso(cur["observed_ts"]),
                    "amount": round(delta, 6)}, account))
    out["latest"] = [dict(points[-1]) for points in series.values() if points]
    return out

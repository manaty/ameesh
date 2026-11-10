# SPDX-License-Identifier: AGPL-3.0-only
"""Profil de contexte observé — ``ameesh-context/1`` (CTX-03).

Ce module publie les faits de profil que le contrat
(docs/design/ctx-01-compteurs-contexte.md, §3 et §4) sépare des compteurs et
des décisions legacy :

* ``model_effective`` : le modèle **annoncé par le harnais** pour le fil
  principal, ou — à défaut d'annonce — le modèle demandé au lancement, publié
  avec ``model_quality=requested`` et jamais ``measured`` ;
* ``window_tokens`` : la fenêtre runtime de ce modèle, jamais celle d'un
  modèle auxiliaire, d'un modèle précédent ou du catalogue ;
* l'état d'une compaction native : ``pre``/``post``, ``partial`` quand le
  harnais ne publie pas l'après (le contrat interdit d'inventer cet après),
  publié dans ``turn.context.compaction`` ;
* :func:`publish` : la section ``profile`` de ``turn.context`` (CTX-03b), qui
  ajoute au profil du tour le harnais et sa version, le mode et la classe de
  rôle du canon — pour un tour résident comme non résident ;
* :func:`adapter_snapshot` : la section ``adapter`` de ``turn.context`` (M4,
  #6662), le réglage **réellement remis à l'adaptateur** (modèle, effort,
  version du binaire, compte de facturation par identifiant non secret) et sa
  qualité ``requested``/``effective``/``unknown`` ; :func:`adapter_view` la
  relit sans combler un trou (un événement N-1 n'a pas ce bloc).

**Observation seule.** Rien ici ne décide d'une rotation ni d'un compactage,
et aucune valeur legacy n'est requalifiée : ``resident`` garde son chemin de
décision (``_window``) à l'identique ; ce module alimente la télémétrie du
tour. Une valeur inconnue reste ``None`` avec une raison, jamais ``0`` ni une
valeur supposée.

Le profil complet du contrat est ``(modèle, harnais, version, mode, rôle,
compte)``. Un driver résident vit avec un harnais, une version, un mode et un
compte fixes : la seule bascule observable dans sa vie est celle du modèle
effectif, et c'est celle que :func:`advance` invalide. Un redémarrage du
worker repart d'un profil vide, donc d'une fenêtre inconnue jusqu'à la
prochaine annonce runtime.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from .context_metrics import (DERIVED, FALLBACK, MEASURED, PARTIAL, REQUESTED,
                              UNKNOWN)

SCHEMA = "ameesh-context/1"

#: M4 (#6662) : le réglage réellement remis à l'adaptateur pour le tour —
#: effort, modèle, version du binaire et compte de facturation par identifiant
#: non secret. Bloc **additif** de ``turn.context``, à côté de ``profile`` : un
#: événement antérieur n'en porte pas, et ses trous se lisent ``unknown``.
ADAPTER_SCHEMA = "ameesh-adapter/1"

#: qualités d'un réglage d'adaptateur, au sens de l'étude #6650 :
#: ``requested`` (seul ce qu'ameesh a passé est connu), ``effective`` (le
#: harnais l'a confirmé au runtime), ``unknown`` (aucune des deux preuves).
#: Le vocabulaire est distinct de ``model_quality`` du profil, qui garde
#: ``measured``/``requested``/``unknown`` pour ne pas requalifier le contrat
#: ``ameesh-context/1`` existant.
EFFECTIVE = "effective"
ADAPTER_QUALITIES = (REQUESTED, EFFECTIVE, UNKNOWN)

#: modes de compte du contrat §3.1, dérivés du type de profil déclaré.
CREDENTIAL_MODES = ("subscription", "api-key")

#: sources runtime de la fenêtre (§3.1, §3.5)
SOURCE_MODEL_USAGE = "runtime:modelUsage.contextWindow"
SOURCE_CODEX = "runtime:modelContextWindow"
SOURCE_ACP = "runtime:usage_update.size"

#: raisons publiées avec une valeur absente. Stables et comptables : un
#: consommateur ne les traduit pas en décision.
REASON_MODEL_UNKNOWN = "model_unknown"
REASON_MODEL_AMBIGUOUS = "model_ambiguous"
REASON_ALIAS_UNKNOWN = "alias_unknown"
REASON_ALIAS_AMBIGUOUS = "alias_ambiguous"
REASON_WINDOW_UNKNOWN = "window_unknown"
REASON_VERSION_UNKNOWN = "version_unknown"
REASON_MODE_UNKNOWN = "mode_unknown"
REASON_PRE_UNKNOWN = "pre_tokens_unknown"
REASON_POST_UNKNOWN = "post_tokens_unknown"

#: qualités admises pour le modèle effectif (contrat §3.1) : le modèle
#: **demandé** n'est jamais ``measured`` s'il n'a pas été annoncé.
MODEL_QUALITIES = (MEASURED, DERIVED, FALLBACK, PARTIAL, UNKNOWN, "requested")

#: classes de rôle du contrat §3.1/§3.6. Le canon porte des noms plus fins
#: (`implementation`, `relecture`, `orchestrateur`, `github-watch`…) : le
#: profil publie la classe de politique, jamais l'identité de l'agent.
ROLE_PILOT = "pilot"
ROLE_REVIEW = "review"
ROLE_AUDIT = "audit"
ROLE_BUILD = "build"
ROLE_DOCS = "docs"
ROLE_WATCH = "watch"
ROLE_OTHER = "other"
ROLES = (ROLE_PILOT, ROLE_REVIEW, ROLE_AUDIT, ROLE_BUILD, ROLE_DOCS,
         ROLE_WATCH, ROLE_OTHER)

#: premier rôle déclaré au canon → classe de politique. Une valeur inconnue
#: ne devient jamais une supposition : elle retombe sur ``other`` (§3.6).
ROLE_BY_CANON = {
    "orchestrateur": ROLE_PILOT,
    "orchestrator": ROLE_PILOT,
    "pilote": ROLE_PILOT,
    "pilot": ROLE_PILOT,
    "relecture": ROLE_REVIEW,
    "review": ROLE_REVIEW,
    "reviewer": ROLE_REVIEW,
    "qa": ROLE_REVIEW,
    "auditeur": ROLE_AUDIT,
    "auditor": ROLE_AUDIT,
    "audit": ROLE_AUDIT,
    "implementation": ROLE_BUILD,
    "implémentation": ROLE_BUILD,
    "build": ROLE_BUILD,
    "composition": ROLE_BUILD,
    "composeur": ROLE_BUILD,
    "documentation": ROLE_DOCS,
    "docs": ROLE_DOCS,
    "release-notes": ROLE_DOCS,
    "github-watch": ROLE_WATCH,
    "veille": ROLE_WATCH,
    "watch": ROLE_WATCH,
    "other": ROLE_OTHER,
}


def role_of(roles) -> str:
    """La classe de politique du **premier** rôle déclaré au canon.

    Le canon ordonne les rôles d'une fiche (``[implementation, relecture]``,
    ``[orchestrateur, conception]``) : le premier est le rôle principal, et
    c'est celui que le profil date. Une fiche sans rôle, ou dont aucun rôle
    n'est reconnu, vaut ``other`` — le profil ne devine pas (§3.6). Un rôle
    déjà nommé dans le vocabulaire du contrat est accepté tel quel.
    """
    if isinstance(roles, str):
        roles = [roles]
    if isinstance(roles, (set, frozenset)):
        # un ensemble n'a pas d'ordre : le tri rend le profil reproductible
        roles = sorted(roles, key=lambda item: str(item))
    if not isinstance(roles, (list, tuple)):
        return ROLE_OTHER
    for role in roles:
        name = str(role or "").strip().lower()
        if not name:
            continue
        if name in ROLES:
            return name
        # Le premier rôle **déclaré** est le principal : s'il n'appartient
        # pas au vocabulaire du contrat, le profil ne saute pas au suivant
        # pour se fabriquer une politique.
        return ROLE_BY_CANON.get(name, ROLE_OTHER)
    return ROLE_OTHER


def role_classes(roles) -> tuple[str, ...]:
    """Les classes de politique de **tous** les rôles déclarés (#6046).

    Contrairement à :func:`role_of` (le rôle principal, que le profil date),
    un budget par rôle (`session.by_role`) doit voir chaque rôle de la fiche :
    la valeur la plus stricte l'emporte. Ordre de déclaration, sans doublon ;
    un rôle hors vocabulaire vaut ``other``, une fiche sans rôle ``(other,)``.
    """
    if isinstance(roles, str):
        roles = [roles]
    if isinstance(roles, (set, frozenset)):
        roles = sorted(roles, key=lambda item: str(item))
    out: list[str] = []
    for role in roles if isinstance(roles, (list, tuple)) else ():
        name = str(role or "").strip().lower()
        if not name:
            continue
        klass = name if name in ROLES else ROLE_BY_CANON.get(name, ROLE_OTHER)
        if klass not in out:
            out.append(klass)
    return tuple(out) or (ROLE_OTHER,)


def _name(value) -> str | None:
    """Un nom de modèle non vide, ou ``None`` (jamais une chaîne vide)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _finite_int(value) -> int | None:
    """L'entier d'un réel **fini**, ou ``None`` (#4892 N2).

    ``json.loads`` accepte ``NaN``/``Infinity`` : ``int()`` y lève
    ``ValueError``/``OverflowError``. Une métadonnée non finie vaut
    « inconnue » (``None``), jamais une exception.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value)


def _positive(value) -> int | None:
    """Un entier de mesure strictement positif, ou ``None``.

    Une fenêtre à zéro n'existe pas ; elle vaut « inconnue », jamais ``0``.
    """
    value = _finite_int(value)
    if value is not None and value > 0:
        return value
    return None


def _token(value) -> int | None:
    """Un jeton de compaction borné à zéro, ou ``None`` (contrat §2.4)."""
    value = _finite_int(value)
    return None if value is None else max(value, 0)


def _first_token(meta: Mapping, *names: str) -> int | None:
    for name in names:
        value = _token(meta.get(name))
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class AliasTable:
    """Table d'alias **explicite et versionnée** (contrat §3.1, §7.4).

    Aucune table n'est publiée à ce jour : un alias annoncé reste donc non
    résolu, et ``W`` reste ``None``. Une table fournie par un appelant ne vaut
    que pour la version qu'elle déclare ; une cible ambiguë (plusieurs
    modèles) n'est jamais tranchée par ce module.
    """

    version: int = 0
    entries: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def resolve(self, alias: str | None) -> tuple[str | None, str | None]:
        """Le modèle cible unique de ``alias``, ou ``(None, raison)``."""
        name = _name(alias)
        if name is None:
            return None, REASON_ALIAS_UNKNOWN
        found = tuple(self.entries.get(name, ()))
        if len(found) == 1:
            target = _name(found[0])
            if target is not None:
                return target, None
        if len(found) > 1:
            return None, REASON_ALIAS_AMBIGUOUS
        return None, REASON_ALIAS_UNKNOWN


#: Aucune table d'alias publiée (contrat §7.4 : inconnue nommée).
NO_ALIASES = AliasTable()


@dataclass(frozen=True)
class Window:
    """La fenêtre observée du modèle effectif, avec sa qualité et sa raison."""

    model_effective: str | None = None
    model_quality: str = UNKNOWN
    window_tokens: int | None = None
    window_quality: str = UNKNOWN
    window_source: str = ""
    reasons: tuple[str, ...] = ()
    auxiliary_models: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """La fenêtre est-elle mesurée ?"""
        return self.window_tokens is not None

    def to_dict(self) -> dict:
        """La forme ``ameesh-context/1`` publiée dans la télémétrie du tour."""
        return {
            "schema": SCHEMA,
            "model_effective": self.model_effective,
            "model_quality": self.model_quality,
            "window_tokens": self.window_tokens,
            "window_quality": self.window_quality,
            "window_source": self.window_source or None,
            "reasons": list(self.reasons),
            "auxiliary_models": list(self.auxiliary_models),
        }


@dataclass(frozen=True)
class Compaction:
    """Une compaction native observée (contrat §4).

    ``post_tokens`` absent ne vaut pas zéro : la mesure d'après reste inconnue
    (``partial``), et :attr:`context_tokens` garde l'avant pour que la
    prochaine mesure tranche.
    """

    pre_tokens: int | None = None
    post_tokens: int | None = None
    quality: str = UNKNOWN
    reasons: tuple[str, ...] = ()
    trigger: str | None = None
    #: #6044 : nombre de compactions natives observées dans le tour. Un item
    #: ``compact_boundary`` ou un `thread/compacted` en vaut une ; le journal
    #: Codex peut en compter plusieurs et les deux preuves d'une même
    #: compaction ne comptent qu'une fois (cf. ``cost.codex_turn_last_call_usage``).
    count: int = 1

    @property
    def context_tokens(self) -> int | None:
        """L'après s'il est prouvé, sinon l'avant (jamais une invention)."""
        return self.post_tokens if self.post_tokens is not None else self.pre_tokens

    def to_dict(self) -> dict:
        """La forme gardée dans ``TurnResult.compact`` (clés legacy ``pre``/``post``)."""
        return {
            "pre": self.pre_tokens,
            "post": self.post_tokens,
            "quality": self.quality,
            "reasons": list(self.reasons),
            "trigger": self.trigger,
            "count": max(int(self.count), 1),
        }


def from_model_usage(model_usage: Mapping | None, *, announced: str | None = None,
                     aliases: AliasTable = NO_ALIASES,
                     source: str = SOURCE_MODEL_USAGE) -> Window:
    """Résout le modèle principal effectif dans un ``modelUsage`` de harnais.

    Le modèle principal est celui que le harnais a **annoncé** (évenement de
    session, ``result``) ; il n'est jamais choisi par la première entrée de
    ``modelUsage``, qui peut être un modèle auxiliaire (contrat §5.4 R1). Un
    alias n'est résolu que par ``aliases`` ; sans correspondance unique, le
    modèle et la fenêtre restent ``None``.

    Sans annonce de modèle, le profil reste inconnu même si une seule entrée
    existe : une entrée unique peut être un auxiliaire, et le contrat interdit
    de la prendre pour le fil principal. Un ``modelUsage`` absent avec un
    modèle annoncé garde le modèle mais laisse la fenêtre inconnue : c'est
    :func:`advance` qui décide si la mesure précédente du même profil reste
    valable.
    """
    entries: dict[str, Mapping] = {}
    if isinstance(model_usage, Mapping):
        entries = {str(name): entry for name, entry in model_usage.items()
                   if isinstance(entry, Mapping)}
    name = _name(announced)
    if name is None:
        reason = REASON_MODEL_AMBIGUOUS if len(entries) > 1 else REASON_MODEL_UNKNOWN
        return Window(None, UNKNOWN, None, UNKNOWN, source,
                      (reason,), tuple(sorted(entries)))
    if not entries:
        return Window(name, MEASURED, None, UNKNOWN, source,
                      (REASON_WINDOW_UNKNOWN,), ())

    reasons: list[str] = []
    if name in entries:
        model, quality = name, MEASURED
    else:
        target, why = aliases.resolve(name)
        if target is not None and target in entries:
            model, quality = target, DERIVED
        else:
            reasons.append(why or REASON_ALIAS_UNKNOWN)
            return Window(None, UNKNOWN, None, UNKNOWN, source,
                          tuple(reasons), tuple(sorted(entries)))

    window = _positive(entries[model].get("contextWindow"))
    if window is None:
        reasons.append(REASON_WINDOW_UNKNOWN)
    return Window(model, quality, window,
                  MEASURED if window is not None else UNKNOWN,
                  source, tuple(reasons),
                  tuple(sorted(other for other in entries if other != model)))


def from_runtime_window(tokens, *, model: str | None = None,
                        source: str = SOURCE_CODEX) -> Window:
    """La fenêtre publiée directement par un harnais (Codex, ACP).

    Le modèle reste celui annoncé par le harnais ; sans lui, la fenêtre n'est
    pas rattachable à un profil et reste inconnue.
    """
    name = _name(model)
    window = _positive(tokens)
    reasons: list[str] = []
    if name is None:
        reasons.append(REASON_MODEL_UNKNOWN)
    if window is None:
        reasons.append(REASON_WINDOW_UNKNOWN)
    if name is None or window is None:
        return Window(name, UNKNOWN if name is None else MEASURED,
                      None, UNKNOWN, source, tuple(reasons), ())
    return Window(name, MEASURED, window, MEASURED, source)


def advance(previous: Window | None, observed: Window) -> Window:
    """Le profil du tour : une bascule de modèle invalide la fenêtre d'avant.

    * même modèle, métadonnée non ré-annoncée ce tour : la fenêtre déjà
      mesurée reste valable (un driver ne redémarre pas à chaque tour) ;
    * modèle différent : seule la nouvelle observation compte, et une fenêtre
      absente reste ``None`` — jamais la valeur de l'ancien modèle ;
    * modèle inconnu : ``None``, sans reprise.
    """
    if previous is None or observed.model_effective is None:
        return observed
    if previous.model_effective is None:
        return observed
    if observed.model_effective != previous.model_effective:
        return observed
    if observed.window_tokens is not None or previous.window_tokens is None:
        return observed
    return replace(previous, model_quality=observed.model_quality)


def _window_dict(observed) -> dict:
    """La forme dict d'une fenêtre observée, quelle que soit son origine."""
    if isinstance(observed, Window):
        return observed.to_dict()
    if isinstance(observed, Mapping):
        return dict(observed)
    return {}


def _reason_list(value) -> list[str]:
    """Les raisons non vides d'un profil, dans leur ordre."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _setting_quality(requested, effective) -> str:
    """``effective`` quand le harnais a confirmé, ``requested`` sinon si une
    valeur a été passée, ``unknown`` sans aucune des deux preuves."""
    if _name(effective) is not None:
        return EFFECTIVE
    if _name(requested) is not None:
        return REQUESTED
    return UNKNOWN


def account_identity(account) -> dict:
    """L'identité **non secrète** d'un compte de facturation, ou des nuls.

    Seuls le nom déclaré du profil, l'identifiant de facturation et le mode de
    compte (``subscription``/``api-key``) sortent d'ici. Un chemin, une
    variable d'environnement, un fichier de clé ou une clé n'entrent jamais
    dans une publication : ``turn.context`` est partagé (M4, #6662).

    ``account`` est un profil de compte (``accounts.Profile``) ou ``None`` ;
    un objet sans les champs attendus rend des nuls, jamais une exception.
    """
    if account is None:
        return {"account_name": None, "billing_account": None,
                "credential_mode": None}
    kind = str(getattr(account, "kind", "") or "").strip().lower()
    mode = {"api_key_env": "api-key", "config_dir": "subscription"}.get(kind)
    return {
        "account_name": _name(getattr(account, "name", None)),
        "billing_account": _name(getattr(account, "billing_account", None)),
        "credential_mode": mode if mode in CREDENTIAL_MODES else None,
    }


def adapter_snapshot(*, harness: str | None = None,
                     own_harness: str | None = None,
                     borrowed: bool | None = None,
                     model_requested: str | None = None,
                     model_effective: str | None = None,
                     effort_requested: str | None = None,
                     effort_effective: str | None = None,
                     harness_version: str | None = None,
                     account_name: str | None = None,
                     billing_account: str | None = None,
                     credential_mode: str | None = None) -> dict:
    """La section ``adapter`` de ``turn.context`` (M4, #6662).

    Elle décrit le réglage **réellement remis à l'adaptateur** pour le tour,
    pas le réglage courant du registre : ``model_requested`` et
    ``effort_requested`` sont les valeurs passées à ``adapter.command()`` (ou
    écrites par son gabarit) ; ``model_effective`` n'est rempli que sur une
    preuve runtime (modèle annoncé par le flux ou lu dans le journal du
    harnais), jamais avec le seul nom demandé. Chaque réglage porte sa qualité
    ``requested``/``effective``/``unknown``.

    ``borrowed`` dit que le harnais (et donc son compte) est un repli de la
    politique L242b : le compte publié est celui du repli, pas celui de la
    fiche. ``own_harness`` garde le harnais déclaré, pour que l'écart reste
    lisible. ``harness_version`` vient de ``harness_versions.probe`` ; une
    version absente reste ``None`` avec ``version_quality=unknown``.

    Aucun secret, aucun prompt : ``account_name``/``billing_account`` sont des
    identifiants non secrets, ``credential_mode`` un mode, jamais une clé.
    """
    requested_model = _name(model_requested)
    effective_model = _name(model_effective)
    requested_effort = _name(effort_requested)
    effective_effort = _name(effort_effective)
    version = _name(harness_version)
    name = _name(account_name)
    billing = _name(billing_account)
    mode = credential_mode if credential_mode in CREDENTIAL_MODES else None
    return {
        "schema": ADAPTER_SCHEMA,
        "harness": _name(harness),
        "own_harness": _name(own_harness),
        "borrowed": None if borrowed is None else bool(borrowed),
        "version": version,
        "version_quality": EFFECTIVE if version is not None else UNKNOWN,
        "model": {
            "requested": requested_model,
            "effective": effective_model,
            "quality": _setting_quality(requested_model, effective_model),
        },
        "effort": {
            "requested": requested_effort,
            "effective": effective_effort,
            "quality": _setting_quality(requested_effort, effective_effort),
        },
        "account": {
            "name": name,
            "billing_account": billing,
            "credential_mode": mode,
            "quality": REQUESTED if (name or billing) else UNKNOWN,
        },
    }


def adapter_view(document: Mapping | None) -> dict:
    """Relit ``document["adapter"]`` sans inventer un trou (M4, #6662).

    Un événement antérieur à M4 n'a pas de bloc ``adapter`` : tous ses
    réglages restent ``None`` avec la qualité ``unknown``, jamais reconstruits
    depuis le registre ou le catalogue courant (aucun backfill). Un champ
    malformé est traité de même, champ par champ. Un consommateur (M1) lit
    donc les trous au lieu de les confondre avec une valeur mesurée.

    ``document`` est le document ``ameesh-context/1`` (``payload["context"]``
    d'un événement ``turn.context``), pas l'événement entier.
    """
    raw = document.get("adapter") if isinstance(document, Mapping) else None
    if not isinstance(raw, Mapping):
        raw = {}

    def _obj(name: str) -> Mapping:
        value = raw.get(name)
        return value if isinstance(value, Mapping) else {}

    model = _obj("model")
    effort = _obj("effort")
    account = _obj("account")
    borrowed = raw.get("borrowed")
    return adapter_snapshot(
        harness=raw.get("harness"),
        own_harness=raw.get("own_harness"),
        borrowed=borrowed if isinstance(borrowed, bool) else None,
        model_requested=model.get("requested"),
        model_effective=model.get("effective"),
        effort_requested=effort.get("requested"),
        effort_effective=effort.get("effective"),
        harness_version=raw.get("version"),
        account_name=account.get("name"),
        billing_account=account.get("billing_account"),
        credential_mode=account.get("credential_mode"))


def publish(observed: Window | Mapping | None = None, *,
            requested_model: str | None = None,
            harness: str | None = None,
            harness_version: str | None = None,
            mode: str | None = None,
            roles=None, role: str | None = None) -> dict:
    """La section ``profile`` publiée dans ``turn.context`` (contrat §3.1).

    ``observed`` est la fenêtre runtime du tour — :class:`Window` pour un tour
    non résident, le dict déjà publié par un driver résident — ou ``None``.
    ``requested_model`` est le modèle **demandé** au lancement : sans annonce
    du harnais, il remplit ``model_effective`` avec la qualité ``requested``,
    jamais ``measured`` (§3.1) ; il ne donne jamais de fenêtre. Une fenêtre
    ``None`` reste ``None`` avec ``window_unknown`` : jamais la fenêtre d'un
    modèle auxiliaire ou précédent (R1, §3.3).

    ``harness_version`` vient de ``harness_versions.probe``, enregistrée par
    hôte ; illisible elle reste ``None`` avec ``version_unknown`` dans
    ``version_reasons``. ``role`` explicite prime, sinon le premier rôle de
    ``roles`` est mappé par :func:`role_of`. Une valeur inconnue ne devient
    jamais une supposition. ``reasons`` ne porte que les absences du modèle et
    de la fenêtre : un consommateur les attache à ``W``.
    """
    raw = _window_dict(observed)
    reasons = _reason_list(raw.get("reasons"))

    model = _name(raw.get("model_effective"))
    model_quality = raw.get("model_quality")
    if model_quality not in MODEL_QUALITIES:
        model_quality = MEASURED if model is not None else UNKNOWN
    if model is None:
        requested = _name(requested_model)
        if requested is not None:
            # Le registre/lancement est une entrée, pas une annonce runtime :
            # la qualité le dit, et aucune fenêtre n'en est déduite.
            model, model_quality = requested, "requested"
        else:
            model_quality = UNKNOWN
            reasons.append(REASON_MODEL_UNKNOWN)

    window = _positive(raw.get("window_tokens"))
    if window is None:
        window_quality = UNKNOWN
        window_source = None
        reasons.append(REASON_WINDOW_UNKNOWN)
    else:
        window_quality = raw.get("window_quality")
        if window_quality not in (MEASURED, DERIVED, FALLBACK, PARTIAL, UNKNOWN):
            window_quality = MEASURED
        window_source = _name(raw.get("window_source"))

    version = _name(harness_version)
    # `reasons` reste réservé au modèle et à la fenêtre (le consommateur
    # `ameesh-context-corpus/1` les attache à `W`) ; les absences du harnais
    # ont leurs propres listes, pour ne pas salir la provenance de `W`.
    version_reasons = [] if version is not None else [REASON_VERSION_UNKNOWN]
    mode_value = _name(mode) or _name(raw.get("mode"))
    mode_reasons = [] if mode_value is not None else [REASON_MODE_UNKNOWN]
    explicit_role = _name(role)
    if explicit_role is not None:
        explicit_role = explicit_role.lower()
        role_value = explicit_role if explicit_role in ROLES \
            else ROLE_BY_CANON.get(explicit_role, ROLE_OTHER)
    else:
        role_value = role_of(roles)

    auxiliary = raw.get("auxiliary_models")
    auxiliary_models = [str(name) for name in auxiliary if _name(name)] \
        if isinstance(auxiliary, (list, tuple, set, frozenset)) else []

    return {
        "schema": SCHEMA,
        "model_effective": model,
        "model_quality": model_quality,
        "harness": _name(harness) or _name(raw.get("harness")),
        "harness_version": version,
        "harness_version_quality": MEASURED if version is not None else UNKNOWN,
        "version_reasons": version_reasons,
        "mode": mode_value,
        "mode_reasons": mode_reasons,
        "role": role_value,
        "window_tokens": window,
        "window_quality": window_quality,
        "window_source": window_source,
        "reasons": list(dict.fromkeys(reasons)),
        "auxiliary_models": auxiliary_models,
    }


def compaction_from_parts(pre_tokens: int | None, post_tokens: int | None,
                          *, count: int = 1, trigger: str | None = None,
                          pre_reason: str = REASON_PRE_UNKNOWN,
                          post_reason: str = REASON_POST_UNKNOWN,
                          extra_reasons: Iterable[str] = ()) -> Compaction:
    """Une compaction observée dont les jetons avant/après sont déjà connus.

    #6044 : le journal Codex donne le dernier contexte d'avant et le premier
    d'après, sans ``post_tokens`` nommé. Une compaction demandée n'entre pas
    ici : l'appelant ne construit le bloc que sur une preuve de réussite.
    ``extra_reasons`` porte les inconnues de frontière (un appel illisible
    entre deux compactions, par exemple) : une valeur connue reste alors un
    minorant, et la qualité est ``partial``.
    """
    reasons: list[str] = []
    if pre_tokens is None:
        reasons.append(pre_reason)
    if post_tokens is None:
        reasons.append(post_reason)
    for reason in extra_reasons or ():
        reason = str(reason)
        if reason and reason not in reasons:
            reasons.append(reason)
    if post_tokens is None:
        quality = PARTIAL if pre_tokens is not None else UNKNOWN
    elif reasons:
        quality = PARTIAL
    else:
        quality = MEASURED
    return Compaction(pre_tokens, post_tokens, quality, tuple(reasons), trigger,
                      max(int(count), 1))


def compaction(event: Mapping | None) -> Compaction:
    """Lit une compaction native (``compact_boundary`` de Claude).

    ``compact_metadata`` porte ``pre_tokens``/``post_tokens`` ; certains
    événements les portent à la racine. Sans ``post_tokens``, la qualité est
    ``partial`` et l'après reste ``None`` (contrat §4.2, §6.2).
    """
    meta = event if isinstance(event, Mapping) else {}
    embedded = meta.get("compact_metadata")
    if isinstance(embedded, Mapping):
        meta = embedded
    pre = _first_token(meta, "pre_tokens", "preTokens", "pre")
    post = _first_token(meta, "post_tokens", "postTokens", "post")
    trigger = meta.get("trigger") if isinstance(meta.get("trigger"), str) else None
    return compaction_from_parts(pre, post, trigger=trigger)

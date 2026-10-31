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
  harnais ne publie pas l'après (le contrat interdit d'inventer cet après) ;
* :func:`publish` : la section ``profile`` de ``turn.context`` (CTX-03b), qui
  ajoute au profil du tour le harnais et sa version, le mode et la classe de
  rôle du canon — pour un tour résident comme non résident.

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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from .context_metrics import DERIVED, FALLBACK, MEASURED, PARTIAL, UNKNOWN

SCHEMA = "ameesh-context/1"

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
    reasons: list[str] = []
    if pre is None:
        reasons.append(REASON_PRE_UNKNOWN)
    if post is None:
        reasons.append(REASON_POST_UNKNOWN)
    quality = MEASURED if post is not None else (PARTIAL if pre is not None else UNKNOWN)
    trigger = meta.get("trigger") if isinstance(meta.get("trigger"), str) else None
    return Compaction(pre, post, quality, tuple(reasons), trigger)
